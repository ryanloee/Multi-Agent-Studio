"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { api } from "@/lib/api";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { WorkflowDetail } from "@/types/api";

import Toolbar from "@/components/toolbar/Toolbar";
import Sidebar from "@/components/sidebar/Sidebar";
import FlowCanvas from "@/components/canvas/FlowCanvas";
import ConfigPanel from "@/components/panels/ConfigPanel";
import OutputPanel from "@/components/panels/OutputPanel";
import ApprovalModal from "@/components/panels/ApprovalModal";

// ---------------------------------------------------------------------------
// WorkflowEditor — three-column layout with toolbar
//
// Layout:
// ┌──────────────────────────────────────────────┐
// │ Toolbar (h-12)                               │
// ├──────┬────────────────────┬──────────────────┤
// │ Side │  FlowCanvas        │  ConfigPanel     │
// │ 240px│  (flex-1)          │  (w-320px)       │
// │      ├────────────────────┤                  │
// │      │  OutputPanel       │                  │
// │      │  (h-300px, 折36px) │                  │
// └──────┴────────────────────┴──────────────────┘
// ---------------------------------------------------------------------------
export default function WorkflowEditor() {
  const params = useParams();
  const workflowId = params.id as string;
  const t = useLocaleStore((s) => s.t);

  // ---- Local state ----
  const [workflowName, setWorkflowName] = useState(t("wfEditor.untitled"));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // ---- Stores ----
  const loadWorkflow = useWorkflowStore((s) => s.loadWorkflow);
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const edges = useWorkflowStore((s) => s.edges) ?? [];
  const selectedNodeId = useWorkflowStore((s) => s.selectedNodeId);

  const runId = useRunStore((s) => s.runId);
  const runStatus = useRunStore((s) => s.status);
  const setRunId = useRunStore((s) => s.setRunId);
  const setStatus = useRunStore((s) => s.setStatus);
  const clearEvents = useRunStore((s) => s.clearEvents);

  // ---- WebSocket connection ----
  useWebSocket(runId);

  // ---- Load workflow data on mount ----
  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);
      try {
        const data: WorkflowDetail = await api.getWorkflow(workflowId);
        if (cancelled) return;
        setWorkflowName(data.name);
        loadWorkflow(data);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load workflow");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [workflowId, loadWorkflow]);

  // ---- Debounced auto-save (2 seconds) ----
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const latestNameRef = useRef(workflowName);
  latestNameRef.current = workflowName;

  const triggerAutoSave = useCallback(() => {
    if (saveTimerRef.current !== null) {
      clearTimeout(saveTimerRef.current);
    }
    saveTimerRef.current = setTimeout(async () => {
      try {
        await api.updateWorkflow(workflowId, {
          name: latestNameRef.current,
          nodes,
          edges,
        });
      } catch (err) {
        console.error("Auto-save failed:", err);
      }
    }, 2000);
  }, [workflowId, nodes, edges]);

  // Auto-save when nodes or edges change (debounced 2s)
  useEffect(() => {
    // Don't auto-save during initial load
    if (loading) return;
    triggerAutoSave();
    return () => {
      if (saveTimerRef.current !== null) {
        clearTimeout(saveTimerRef.current);
      }
    };
  }, [nodes, edges, loading, triggerAutoSave]);

  // ---- Name change handler ----
  const handleNameChange = useCallback(
    (name: string) => {
      setWorkflowName(name);
    },
    []
  );

  // ---- Save handler ----
  const handleSave = useCallback(async () => {
    try {
      await api.updateWorkflow(workflowId, {
        name: workflowName,
        nodes,
        edges,
      });
    } catch (err) {
      console.error("Save failed:", err);
    }
  }, [workflowId, workflowName, nodes, edges]);

  // ---- Cleanup on unmount ----
  useEffect(() => {
    return () => {
      // Reset stores when leaving the editor
      setRunId(null);
      setStatus("idle");
      clearEvents();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- Loading state ----
  if (loading) {
    return (
      <div className="h-screen w-screen flex items-center justify-center bg-gray-50">
        <div className="text-center space-y-3">
          <div className="w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full animate-spin mx-auto" />
          <p className="text-sm text-gray-500">{t("wfEditor.loading")}</p>
        </div>
      </div>
    );
  }

  // ---- Error state ----
  if (error) {
    return (
      <div className="h-screen w-screen flex items-center justify-center bg-gray-50">
        <div className="text-center space-y-3">
          <p className="text-red-500 font-medium">{t("wfEditor.loadFailed")}</p>
          <p className="text-sm text-gray-400">{error}</p>
          <button
            onClick={() => window.location.reload()}
            className="px-4 py-2 text-sm bg-blue-500 text-white rounded-md hover:bg-blue-600 transition-colors"
          >
            {t("wfEditor.retry")}
          </button>
        </div>
      </div>
    );
  }

  // ---- Main layout ----
  return (
    <div className="h-screen w-screen flex flex-col overflow-hidden bg-gray-50">
      {/* Top: Toolbar */}
      <Toolbar
        workflowId={workflowId}
        workflowName={workflowName}
        onNameChange={handleNameChange}
        onSave={handleSave}
      />

      {/* Body: Sidebar + Canvas area + ConfigPanel */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: Sidebar (240px) */}
        <Sidebar />

        {/* Center: Canvas + OutputPanel */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Canvas (flex-1) */}
          <div className="flex-1 overflow-hidden">
            <FlowCanvas />
          </div>

          {/* Bottom: OutputPanel (collapsible, h-300 expanded / h-36 collapsed) */}
          <OutputPanel />
        </div>

        {/* Right: ConfigPanel (w-320px, only when a node is selected) */}
        {selectedNodeId && <ConfigPanel />}
      </div>

      {/* Human-in-the-Loop Approval Modal */}
      {runStatus === "paused" && runId && (
        <ApprovalModal runId={runId} />
      )}
    </div>
  );
}
