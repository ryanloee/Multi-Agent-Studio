"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { api } from "@/lib/api";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useSettingsStore } from "@/stores/settingsStore";
import { useLocaleStore } from "@/stores/localeStore";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { WorkflowDetail } from "@/types/api";

import Toolbar from "@/components/toolbar/Toolbar";
import LeftPanel from "@/components/sidebar/LeftPanel";
import FlowCanvas from "@/components/canvas/FlowCanvas";
import ConfigPanel from "@/components/panels/ConfigPanel";
import OutputPanel from "@/components/panels/OutputPanel";
import ApprovalModal from "@/components/panels/ApprovalModal";
import DirectoryPicker from "@/components/common/DirectoryPicker";

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
  const [workspaceDraft, setWorkspaceDraft] = useState("");

  // ---- Stores ----
  const loadWorkflow = useWorkflowStore((s) => s.loadWorkflow);
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const edges = useWorkflowStore((s) => s.edges) ?? [];
  const autoChildModelMap = useWorkflowStore((s) => s.autoChildModelMap);
  const plannerUiState = useWorkflowStore((s) => s.plannerUiState);
  const workspaceDirectory = useWorkflowStore((s) => s.workspaceDirectory);
  const updateWorkspaceDirectory = useWorkflowStore((s) => s.updateWorkspaceDirectory);

  const runId = useRunStore((s) => s.runId);
  const runStatus = useRunStore((s) => s.status);
  const setRunId = useRunStore((s) => s.setRunId);
  const taskCount = useTaskStore((s) => s.tasks.length);
  const setStatus = useRunStore((s) => s.setStatus);
  const clearEvents = useRunStore((s) => s.clearEvents);
  const loadSettings = useSettingsStore((s) => s.loadFromServer);

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
        setWorkspaceDraft(data.workspace_directory ?? "");
        loadWorkflow(data);

        const runs = await api.listRuns(workflowId);
        if (cancelled) return;
        const latestRun = runs[0];
        if (latestRun) {
          useTaskStore.getState().setCurrentRunId(latestRun.id);

          const events = await api.listRunEvents(latestRun.id);
          if (cancelled) return;
          useRunStore.getState().hydrateEvents(events);
          useRunStore.getState().setStatus(latestRun.status);
          setRunId(latestRun.id);
        } else {
          setRunId(null);
          setStatus("idle");
          clearEvents();
          useTaskStore.getState().setCurrentRunId(null);
          useTaskStore.getState().clearTasks();
        }
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
  }, [workflowId, loadWorkflow, setRunId, setStatus, clearEvents]);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  useEffect(() => {
    setWorkspaceDraft(workspaceDirectory ?? "");
  }, [workspaceDirectory]);

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
          metadata: {
            auto_child_model_map: autoChildModelMap,
            planner_ui_state: plannerUiState,
          },
        });
      } catch (err) {
        console.error("Auto-save failed:", err);
      }
    }, 2000);
  }, [workflowId, nodes, edges, autoChildModelMap, plannerUiState]);

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
        metadata: {
          auto_child_model_map: autoChildModelMap,
          planner_ui_state: plannerUiState,
        },
      });
    } catch (err) {
      console.error("Save failed:", err);
    }
  }, [workflowId, workflowName, nodes, edges, autoChildModelMap, plannerUiState]);

  // ---- Cleanup on unmount ----
  useEffect(() => {
    return () => {
      // Reset stores when leaving the editor
      setRunId(null);
      setStatus("idle");
      clearEvents();
      useTaskStore.getState().clearTasks();
      useTaskStore.getState().setCurrentRunId(null);
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

      {/* Body: LeftPanel + Canvas area + ConfigPanel */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: LeftPanel (248px, tabs: 节点库 | 任务) */}
        <LeftPanel workflowId={workflowId} />

        {/* Center: Canvas + OutputPanel */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Canvas (flex-1) */}
          <div className="flex-1 overflow-hidden">
            <FlowCanvas />
          </div>

          {/* Bottom: OutputPanel (collapsible, h-300 expanded / h-36 collapsed) */}
          <OutputPanel />
        </div>

        {/* Right: ConfigPanel (w-320px) */}
        <ConfigPanel />
      </div>

      {/* Human-in-the-Loop Approval Modal */}
      {runStatus === "paused" && runId && (
        <ApprovalModal runId={runId} />
      )}

      {!workspaceDirectory.trim() && (
        <div className="absolute inset-0 z-40 flex items-center justify-center bg-gray-950/45 backdrop-blur-[2px] p-6">
          <div className="w-full max-w-xl rounded-2xl border border-gray-200 bg-white p-6 shadow-2xl">
            <div className="mb-4">
              <h2 className="text-lg font-semibold text-gray-900">先设置工作目录</h2>
              <p className="mt-1 text-sm leading-relaxed text-gray-500">
                进入任何工作流都先绑定项目目录。这样 Planner 的评估、节点执行和产物同步才有明确上下文。
              </p>
            </div>
            <DirectoryPicker
              value={workspaceDraft}
              onChange={setWorkspaceDraft}
              placeholder="输入或浏览项目目录"
              label="工作目录"
            />
            <div className="mt-5 flex items-center justify-end gap-3">
              <button
                onClick={() => window.history.back()}
                className="px-4 py-2 text-sm font-medium text-gray-500 hover:text-gray-700"
              >
                返回
              </button>
              <button
                onClick={() => void updateWorkspaceDirectory(workspaceDraft.trim())}
                disabled={!workspaceDraft.trim()}
                className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                保存并继续
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
