"use client";

import { useState, useCallback, useEffect } from "react";
import {
  Play,
  Square,
  Save,
  Loader2,
  Keyboard,
  Globe,
} from "lucide-react";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import { api } from "@/lib/api";
import { STATUS_COLORS } from "@/lib/constants";
import type { RunStatus } from "@/types/workflow";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
interface ToolbarProps {
  /** Workflow ID — needed for save / run operations */
  workflowId: string;
  /** Current workflow name — editable in the toolbar */
  workflowName: string;
  /** Callback when the user renames the workflow */
  onNameChange: (name: string) => void;
  /** Callback after a successful save */
  onSave?: () => void;
}

// ---------------------------------------------------------------------------
// Toolbar — fixed-height top bar (h-12) with logo, name, save, run controls
// ---------------------------------------------------------------------------
export default function Toolbar({
  workflowId,
  workflowName,
  onNameChange,
  onSave,
}: ToolbarProps) {
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const edges = useWorkflowStore((s) => s.edges) ?? [];

  const runId = useRunStore((s) => s.runId);
  const status = useRunStore((s) => s.status);
  const setRunId = useRunStore((s) => s.setRunId);
  const setStatus = useRunStore((s) => s.setStatus);
  const clearEvents = useRunStore((s) => s.clearEvents);

  const locale = useLocaleStore((s) => s.locale);
  const setLocale = useLocaleStore((s) => s.setLocale);
  const t = useLocaleStore((s) => s.t);

  const [saving, setSaving] = useState(false);
  const [triggering, setTriggering] = useState(false);

  const isRunning = status === "running";
  const isPaused = status === "paused";

  // Status labels derived from i18n
  const STATUS_LABELS: Record<RunStatus, string> = {
    idle: t("toolbar.status.idle"),
    running: t("toolbar.status.running"),
    paused: t("toolbar.status.paused"),
    completed: t("toolbar.status.completed"),
    failed: t("toolbar.status.failed"),
  };

  // ----- Save handler -----
  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await api.updateWorkflow(workflowId, {
        name: workflowName,
        nodes,
        edges,
      });
      onSave?.();
    } catch (err) {
      console.error("Save failed:", err);
    } finally {
      setSaving(false);
    }
  }, [workflowId, workflowName, nodes, edges, onSave]);

  // ----- Run / Cancel handler -----
  const handleRun = useCallback(async () => {
    setTriggering(true);
    try {
      clearEvents();
      const result = await api.triggerRun(workflowId);
      setRunId(result.run_id);
      setStatus("running");
    } catch (err) {
      console.error("Run trigger failed:", err);
      setStatus("failed");
    } finally {
      setTriggering(false);
    }
  }, [workflowId, clearEvents, setRunId, setStatus]);

  const handleCancel = useCallback(async () => {
    if (!runId) return;
    try {
      await api.cancelRun(runId);
      setStatus("idle");
      setRunId(null);
    } catch (err) {
      console.error("Cancel failed:", err);
    }
  }, [runId, setRunId, setStatus]);

  // ----- Keyboard shortcuts: Ctrl+S to save, Ctrl+Enter to run -----
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      // Ctrl+S or Cmd+S -> save
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        handleSave();
      }
      // Ctrl+Enter or Cmd+Enter -> run
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        if (!isRunning && !isPaused) {
          handleRun();
        }
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [handleSave, handleRun, isRunning, isPaused]);

  // ----- Name editing -----
  const [editingName, setEditingName] = useState(false);
  const [tempName, setTempName] = useState(workflowName);

  useEffect(() => {
    setTempName(workflowName);
  }, [workflowName]);

  const commitName = useCallback(() => {
    setEditingName(false);
    const trimmed = tempName.trim();
    if (trimmed && trimmed !== workflowName) {
      onNameChange(trimmed);
    } else {
      setTempName(workflowName);
    }
  }, [tempName, workflowName, onNameChange]);

  // ----- Language toggle -----
  const toggleLocale = useCallback(() => {
    setLocale(locale === "zh" ? "en" : "zh");
  }, [locale, setLocale]);

  // ----- Render -----
  return (
    <header className="h-12 bg-white border-b border-gray-200 flex items-center px-4 shrink-0 select-none">
      {/* ---- Left: Logo ---- */}
      <div className="flex items-center gap-2 mr-6">
        <div className="w-7 h-7 rounded-md bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center">
          <span className="text-white text-xs font-bold">MA</span>
        </div>
        <span className="text-sm font-semibold text-gray-800 hidden sm:inline">
          {t("toolbar.appTitle")}
        </span>
      </div>

      {/* ---- Center: Workflow name (editable) ---- */}
      <div className="flex-1 flex justify-center">
        {editingName ? (
          <input
            autoFocus
            value={tempName}
            onChange={(e) => setTempName(e.target.value)}
            onBlur={commitName}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitName();
              if (e.key === "Escape") {
                setTempName(workflowName);
                setEditingName(false);
              }
            }}
            className="text-sm font-medium text-gray-700 text-center bg-gray-50 border border-gray-300 rounded px-2 py-0.5 focus:outline-none focus:ring-1 focus:ring-blue-400 max-w-[300px]"
          />
        ) : (
          <button
            onClick={() => setEditingName(true)}
            className="text-sm font-medium text-gray-700 hover:text-gray-900 px-2 py-0.5 rounded hover:bg-gray-100 transition-colors max-w-[300px] truncate"
            title={t("toolbar.clickToRename")}
          >
            {workflowName || t("toolbar.untitled")}
          </button>
        )}
      </div>

      {/* ---- Right: Save + Run/Cancel + Status badge + Language toggle ---- */}
      <div className="flex items-center gap-2">
        {/* Save button */}
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-gray-300 text-gray-600 hover:bg-gray-50 transition-colors disabled:opacity-50"
          title={t("toolbar.saveShortcut")}
        >
          {saving ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <Save size={14} />
          )}
          {t("toolbar.save")}
        </button>

        {/* Run / Cancel button */}
        {isRunning || isPaused ? (
          <button
            onClick={handleCancel}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-red-500 text-white hover:bg-red-600 transition-colors"
            title={t("toolbar.cancelShortcut")}
          >
            <Square size={14} />
            {t("toolbar.cancel")}
          </button>
        ) : (
          <button
            onClick={handleRun}
            disabled={triggering}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-green-600 text-white hover:bg-green-700 transition-colors disabled:opacity-50"
            title={t("toolbar.runShortcut")}
          >
            {triggering ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Play size={14} />
            )}
            {t("toolbar.run")}
          </button>
        )}

        {/* Status badge */}
        <span
          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[status]}`}
        >
          {STATUS_LABELS[status]}
        </span>

        {/* Shortcut hint (subtle) */}
        <span className="text-[10px] text-gray-300 hidden lg:inline-flex items-center gap-0.5 ml-1">
          <Keyboard size={10} />
          {t("toolbar.shortcutHint")}
        </span>

        {/* Language toggle */}
        <button
          onClick={toggleLocale}
          className="flex items-center gap-1 px-2 py-1.5 text-xs font-medium rounded-md border border-gray-300 text-gray-600 hover:bg-gray-50 transition-colors ml-1"
          title={locale === "zh" ? "Switch to English" : "切换为中文"}
        >
          <Globe size={14} />
          {locale === "zh" ? "EN" : "中"}
        </button>
      </div>
    </header>
  );
}
