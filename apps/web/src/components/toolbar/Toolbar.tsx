"use client";

import { useState, useCallback, useEffect } from "react";
import {
  Play,
  Square,
  Save,
  Loader2,
  Globe,
  Check,
} from "lucide-react";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import { api } from "@/lib/api";
import { STATUS_COLORS } from "@/lib/constants";
import type { RunStatus } from "@/types/workflow";

interface ToolbarProps {
  workflowId: string;
  workflowName: string;
  onNameChange: (name: string) => void;
  onSave?: () => void;
}

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
  const [saved, setSaved] = useState(false);
  const [triggering, setTriggering] = useState(false);

  const isRunning = status === "running";
  const isPaused = status === "paused";

  const STATUS_LABELS: Record<RunStatus, string> = {
    idle: t("toolbar.status.idle"),
    running: t("toolbar.status.running"),
    paused: t("toolbar.status.paused"),
    completed: t("toolbar.status.completed"),
    failed: t("toolbar.status.failed"),
  };

  const handleSave = useCallback(async () => {
    setSaving(true);
    setSaved(false);
    try {
      await api.updateWorkflow(workflowId, { name: workflowName, nodes, edges });
      setSaved(true);
      onSave?.();
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      console.error("Save failed:", err);
    } finally {
      setSaving(false);
    }
  }, [workflowId, workflowName, nodes, edges, onSave]);

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

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        handleSave();
      }
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        if (!isRunning && !isPaused) handleRun();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [handleSave, handleRun, isRunning, isPaused]);

  const [editingName, setEditingName] = useState(false);
  const [tempName, setTempName] = useState(workflowName);

  useEffect(() => { setTempName(workflowName); }, [workflowName]);

  const commitName = useCallback(() => {
    setEditingName(false);
    const trimmed = tempName.trim();
    if (trimmed && trimmed !== workflowName) onNameChange(trimmed);
    else setTempName(workflowName);
  }, [tempName, workflowName, onNameChange]);

  const toggleLocale = useCallback(() => setLocale(locale === "zh" ? "en" : "zh"), [locale, setLocale]);

  return (
    <header className="h-13 bg-white/95 backdrop-blur-sm border-b border-gray-200 flex items-center px-4 shrink-0 select-none">
      {/* Left: Logo + Back */}
      <div className="flex items-center gap-3 mr-6">
        <a href="/workflows" className="flex items-center gap-2 group">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center shadow-sm group-hover:shadow-md transition-shadow">
            <span className="text-white text-xs font-bold">MA</span>
          </div>
          <span className="text-sm font-semibold text-gray-700 hidden sm:inline group-hover:text-blue-600 transition-colors">
            {t("toolbar.appTitle")}
          </span>
        </a>
      </div>

      {/* Center: Workflow name */}
      <div className="flex-1 flex justify-center">
        {editingName ? (
          <input
            autoFocus
            value={tempName}
            onChange={(e) => setTempName(e.target.value)}
            onBlur={commitName}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitName();
              if (e.key === "Escape") { setTempName(workflowName); setEditingName(false); }
            }}
            className="text-sm font-medium text-gray-700 text-center bg-gray-50 border border-gray-300 rounded-lg px-3 py-1 focus:outline-none focus:ring-2 focus:ring-blue-400 max-w-[300px]"
          />
        ) : (
          <button
            onClick={() => setEditingName(true)}
            className="text-sm font-medium text-gray-700 hover:text-gray-900 px-3 py-1 rounded-lg hover:bg-gray-100 transition-colors max-w-[300px] truncate"
            title={t("toolbar.clickToRename")}
          >
            {workflowName || t("toolbar.untitled")}
          </button>
        )}
      </div>

      {/* Right: Actions */}
      <div className="flex items-center gap-2">
        {/* Save */}
        <button
          onClick={handleSave}
          disabled={saving}
          className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg border transition-all disabled:opacity-50 ${
            saved
              ? "border-green-300 bg-green-50 text-green-700"
              : "border-gray-300 text-gray-600 hover:bg-gray-50 hover:border-gray-400"
          }`}
          title={t("toolbar.saveShortcut")}
        >
          {saving ? (
            <Loader2 size={14} className="animate-spin" />
          ) : saved ? (
            <Check size={14} />
          ) : (
            <Save size={14} />
          )}
          {saved ? t("toolbar.saved") || "已保存" : t("toolbar.save")}
        </button>

        {/* Run / Cancel */}
        {isRunning || isPaused ? (
          <button
            onClick={handleCancel}
            className="flex items-center gap-1.5 px-4 py-1.5 text-xs font-semibold rounded-lg bg-red-500 text-white hover:bg-red-600 transition-colors shadow-sm"
          >
            <Square size={14} />
            {t("toolbar.cancel")}
          </button>
        ) : (
          <button
            onClick={handleRun}
            disabled={triggering || nodes.length === 0}
            className="flex items-center gap-1.5 px-4 py-1.5 text-xs font-semibold rounded-lg bg-gradient-to-r from-green-500 to-emerald-600 text-white hover:from-green-600 hover:to-emerald-700 transition-all shadow-sm disabled:opacity-50"
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
          className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium ${STATUS_COLORS[status]}`}
        >
          {STATUS_LABELS[status]}
        </span>

        {/* Language toggle */}
        <button
          onClick={toggleLocale}
          className="flex items-center gap-1 px-2 py-1.5 text-xs font-medium rounded-lg border border-gray-200 text-gray-500 hover:bg-gray-50 hover:text-gray-700 transition-colors"
          title={locale === "zh" ? "Switch to English" : "切换为中文"}
        >
          <Globe size={14} />
          {locale === "zh" ? "EN" : "中"}
        </button>
      </div>
    </header>
  );
}
