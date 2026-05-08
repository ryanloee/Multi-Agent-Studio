"use client";

import { useCallback, useRef, type DragEvent } from "react";
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
import { nodeTypes } from "@/components/canvas/nodeTypes";
import type { AgentNodeType, WorkflowNode, WorkflowEdge } from "@/types/workflow";
import { NODE_META } from "@/lib/constants";

export default function FlowCanvas() {
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const edges = useWorkflowStore((s) => s.edges) ?? [];
  const onNodesChange = useWorkflowStore((s) => s.onNodesChange);
  const onEdgesChange = useWorkflowStore((s) => s.onEdgesChange);
  const onConnect = useWorkflowStore((s) => s.onConnect);
  const addNode = useWorkflowStore((s) => s.addNode);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);

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

  // ---- Node click: select node ----
  const onNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      setSelectedNode(node.id);
    },
    [setSelectedNode]
  );

  // ---- Pane click: deselect ----
  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
  }, [setSelectedNode]);

  return (
    <div className="w-full h-full relative">
      <ReactFlow
        nodes={nodes}
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
      {nodes.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none select-none">
          <div className="text-center space-y-2">
            <p className="text-gray-400 text-lg font-medium">
              Drag nodes from the sidebar to get started
            </p>
            <p className="text-gray-300 text-sm">
              Connect nodes to build your workflow
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
