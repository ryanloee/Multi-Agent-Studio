"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen } from "lucide-react";
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

// Collapsed rail width (px) — just enough for the toggle button
const RAIL = 32;

export default function WorkflowEditor() {
  const params = useParams();
  const workflowId = params.id as string;
  const t = useLocaleStore((s) => s.t);

  // ---- Panel collapse state ----
  const [leftOpen, setLeftOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(true);

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
        {/* Left rail + panel */}
        {leftOpen ? (
          <LeftPanel workflowId={workflowId} />
        ) : (
          <div
            className="flex flex-col items-center pt-2 border-r border-gray-200 bg-white"
            style={{ width: RAIL }}
          >
            <button
              onClick={() => setLeftOpen(true)}
              className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors"
              title={t("panel.openLeft")}
            >
              <PanelRightOpen size={16} />
            </button>
          </div>
        )}

        {/* Center: Canvas + OutputPanel */}
        <div className="flex-1 flex flex-col overflow-hidden relative">
          {/* Collapse toggles on canvas edges */}
          {leftOpen && (
            <button
              onClick={() => setLeftOpen(false)}
              className="absolute top-2 left-0 z-10 p-1 rounded-r hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors bg-white border border-l-0 border-gray-200"
              title={t("panel.closeLeft")}
            >
              <PanelLeftClose size={14} />
            </button>
          )}
          {rightOpen && (
            <button
              onClick={() => setRightOpen(false)}
              className="absolute top-2 right-0 z-10 p-1 rounded-l hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors bg-white border border-r-0 border-gray-200"
              title={t("panel.closeRight")}
            >
              <PanelRightClose size={14} />
            </button>
          )}

          {/* Canvas (flex-1) */}
          <div className="flex-1 overflow-hidden">
            <FlowCanvas />
          </div>

          {/* Bottom: OutputPanel (collapsible) */}
          <OutputPanel />
        </div>

        {/* Right rail + panel */}
        {rightOpen ? (
          <ConfigPanel />
        ) : (
          <div
            className="flex flex-col items-center pt-2 border-l border-gray-200 bg-white"
            style={{ width: RAIL }}
          >
            <button
              onClick={() => setRightOpen(true)}
              className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors"
              title={t("panel.openRight")}
            >
              <PanelLeftOpen size={16} />
            </button>
          </div>
        )}
      </div>

      {/* Human-in-the-Loop Approval Modal */}
      {runStatus === "paused" && runId && (
        <ApprovalModal runId={runId} />
      )}

      {!workspaceDirectory.trim() && (
        <div className="absolute inset-0 z-40 flex items-center justify-center bg-gray-950/45 backdrop-blur-[2px] p-6">
          <div className="w-full max-w-xl rounded-2xl border border-gray-200 bg-white p-6 shadow-2xl">
            <div className="mb-4">
              <h2 className="text-lg font-semibold text-gray-900">{t("wfEditor.setupDir")}</h2>
              <p className="mt-1 text-sm leading-relaxed text-gray-500">
                {t("wfEditor.setupDirDesc")}
              </p>
            </div>
            <DirectoryPicker
              value={workspaceDraft}
              onChange={setWorkspaceDraft}
              placeholder={t("wfEditor.dirPlaceholder")}
              label={t("wfEditor.workDir")}
            />
            <div className="mt-5 flex items-center justify-end gap-3">
              <button
                onClick={() => window.history.back()}
                className="px-4 py-2 text-sm font-medium text-gray-500 hover:text-gray-700"
              >
                {t("wfEditor.goBack")}
              </button>
              <button
                onClick={() => void updateWorkspaceDirectory(workspaceDraft.trim())}
                disabled={!workspaceDraft.trim()}
                className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {t("wfEditor.saveAndContinue")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
