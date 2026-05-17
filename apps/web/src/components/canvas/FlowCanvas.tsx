"use client";

import { useCallback, useEffect } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useRunStore } from "@/stores/runStore";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useLocaleStore } from "@/stores/localeStore";
import { nodeTypes } from "@/components/canvas/nodeTypes";

// ---------------------------------------------------------------------------
// DAG view — always shown (planning + run phases)
// ---------------------------------------------------------------------------
function DagView() {
  const storeNodes = useWorkflowStore((s) => s.nodes);
  const storeEdges = useWorkflowStore((s) => s.edges);
  const setSelectedRunNode = useRunStore((s) => s.setSelectedRunNode);

  const [nodes, setNodes, onNodesChange] = useNodesState(storeNodes as any[]);
  const [edges, setEdges, onEdgesChange] = useEdgesState(storeEdges as any[]);

  useEffect(() => {
    setNodes(storeNodes as any[]);
  }, [storeNodes, setNodes]);

  useEffect(() => {
    setEdges(storeEdges as any[]);
  }, [storeEdges, setEdges]);

  const onNodeClick = useCallback(
    (_: React.MouseEvent, node: any) => {
      setSelectedRunNode(node.id);
    },
    [setSelectedRunNode],
  );

  if (storeNodes.length === 0) {
    const t = useLocaleStore.getState().t;
    return (
      <div className="w-full h-full flex items-center justify-center bg-white">
        <div className="text-center space-y-3 max-w-sm px-4">
          <div className="w-12 h-12 rounded-xl bg-blue-50 flex items-center justify-center mx-auto">
            <svg className="w-6 h-6 text-blue-400" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
            </svg>
          </div>
          <p className="text-gray-500 text-base font-medium">{t("canvas.autoMode")}</p>
          <p className="text-gray-400 text-sm leading-relaxed">{t("canvas.autoModeDesc")}</p>
        </div>
      </div>
    );
  }

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onNodeClick={onNodeClick}
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={{ padding: 0.2 }}
      proOptions={{ hideAttribution: true }}
    >
      <Background />
      <Controls position="bottom-right" />
      <MiniMap
        nodeStrokeWidth={3}
        zoomable
        pannable
        style={{ border: "1px solid #e5e7eb" }}
      />
    </ReactFlow>
  );
}

// ---------------------------------------------------------------------------
// Main canvas
// ---------------------------------------------------------------------------
function FlowCanvasInner() {
  const goal = useWorkflowStore((s) => s.goal);

  return (
    <div className="w-full h-full flex flex-col bg-white">
      {goal && (
        <div className="px-4 py-3 border-b border-gray-200 bg-gray-50 shrink-0">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">DAG</span>
            <span className="text-sm text-gray-600 truncate">— {goal}</span>
          </div>
        </div>
      )}
      <DagView />
    </div>
  );
}

export default function FlowCanvas() {
  return (
    <ReactFlowProvider>
      <FlowCanvasInner />
    </ReactFlowProvider>
  );
}
