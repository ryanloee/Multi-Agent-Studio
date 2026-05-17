"use client";

import { useCallback, useEffect, useRef } from "react";
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
import type { DirectorDecisionEvent } from "@/types/events";
import type { RunStatus } from "@/types/workflow";

const STATUS_COLORS: Record<string, string> = {
  pending: "bg-gray-200 text-gray-600",
  running: "bg-blue-100 text-blue-700 border-blue-300 animate-pulse",
  completed: "bg-green-100 text-green-700 border-green-300",
  failed: "bg-red-100 text-red-700 border-red-300",
  idle: "bg-gray-100 text-gray-500",
  cancelled: "bg-gray-200 text-gray-500",
};

const ACTION_ICONS: Record<string, string> = {
  scout: "🔍",
  worker: "🔧",
  test: "🧪",
  done: "✅",
  failed: "❌",
};

function formatTimestamp(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ---------------------------------------------------------------------------
// DAG view (planning phase)
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
// Timeline view (run phase)
// ---------------------------------------------------------------------------

function TimelineView() {
  const decisions = useRunStore((s) => s.directorDecisions);
  const events = useRunStore((s) => s.events);
  const setSelectedRunNode = useRunStore((s) => s.setSelectedRunNode);
  const scrollRef = useRef<HTMLDivElement>(null);

  const handleNodeClick = useCallback(
    (nodeId: string) => {
      setSelectedRunNode(nodeId);
    },
    [setSelectedRunNode],
  );

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [decisions, events]);

  const nodeEvents = events.filter(
    (e) =>
      e.type === "node_started" ||
      e.type === "node_completed" ||
      e.type === "node_failed" ||
      e.type === "director_decision",
  );

  return (
    <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-3 space-y-1">
      <div className="relative">
        <div className="absolute left-[19px] top-0 bottom-0 w-0.5 bg-gray-200" />
        {nodeEvents.map((event, idx) => {
          if (event.type === "director_decision") {
            const de = event as DirectorDecisionEvent;
            const icon = ACTION_ICONS[de.action] || "📋";
            return (
              <div key={`decision-${idx}`} className="relative flex items-start gap-3 py-2 group">
                <div className="relative z-10 w-10 h-10 rounded-full bg-white border-2 border-gray-300 flex items-center justify-center text-lg shrink-0">
                  {icon}
                </div>
                <div className="flex-1 min-w-0 pt-1">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-mono text-gray-400">{formatTimestamp(event.timestamp)}</span>
                    <span className="text-xs font-medium px-1.5 py-0.5 rounded bg-purple-100 text-purple-700">{de.action}</span>
                    {de.iteration !== undefined && <span className="text-xs text-gray-400">#{de.iteration}</span>}
                  </div>
                  <p className="text-sm text-gray-700 mt-0.5 line-clamp-2">{de.reasoning}</p>
                  {de.target_files && de.target_files.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-1">
                      {de.target_files.map((f) => (
                        <span key={f} className="text-xs px-1.5 py-0.5 bg-gray-100 text-gray-600 rounded">{f}</span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          }

          const status: RunStatus =
            event.type === "node_started" ? "running" :
            event.type === "node_completed" ? "completed" : "failed";
          const colorClass = STATUS_COLORS[status] || STATUS_COLORS.pending;
          const statusLabel = status === "running" ? "Running" : status === "completed" ? "Done" : "Failed";

          return (
            <div
              key={`node-${idx}`}
              className="relative flex items-start gap-3 py-2 cursor-pointer group hover:bg-gray-50 rounded-lg -mx-2 px-2"
              onClick={() => handleNodeClick(event.node_id)}
            >
              <div className={`relative z-10 w-10 h-10 rounded-full border-2 flex items-center justify-center text-xs font-bold shrink-0 ${colorClass}`}>
                {status === "running" ? "..." : statusLabel[0]}
              </div>
              <div className="flex-1 min-w-0 pt-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-800 truncate">{event.node_id}</span>
                  <span className={`text-xs font-medium px-1.5 py-0.5 rounded ${colorClass}`}>{statusLabel}</span>
                </div>
                <span className="text-xs font-mono text-gray-400">{formatTimestamp(event.timestamp)}</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main canvas — switches between DAG and Timeline
// ---------------------------------------------------------------------------

function FlowCanvasInner() {
  const events = useRunStore((s) => s.events);
  const runStatus = useRunStore((s) => s.status);
  const storeNodes = useWorkflowStore((s) => s.nodes);
  const goal = useWorkflowStore((s) => s.goal);
  const t = useLocaleStore((s) => s.t);

  const hasRunEvents = events.some(
    (e) =>
      e.type === "node_started" ||
      e.type === "node_completed" ||
      e.type === "node_failed" ||
      e.type === "director_decision",
  );
  const isRunning = runStatus === "running" || runStatus === "pending";

  // Show timeline during/after runs; show DAG during planning
  const showTimeline = hasRunEvents || isRunning;

  return (
    <div className="w-full h-full flex flex-col bg-white">
      <div className="px-4 py-3 border-b border-gray-200 bg-gray-50 shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">
            {showTimeline ? "Director" : "DAG"}
          </span>
          {goal && <span className="text-sm text-gray-600 truncate">— {goal}</span>}
        </div>
      </div>

      {showTimeline ? <TimelineView /> : <DagView />}
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
