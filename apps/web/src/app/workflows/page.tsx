"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import { Plus, Trash2, Workflow, Clock, Play, ArrowRight, Target, Settings } from "lucide-react";
import { api } from "@/lib/api";
import { useLocaleStore } from "@/stores/localeStore";
import { useSettingsStore } from "@/stores/settingsStore";
import SettingsModal from "@/components/settings/SettingsModal";
import type { WorkflowSummary, RunInfo } from "@/types/api";

export default function WorkflowListPage() {
  const router = useRouter();
  const t = useLocaleStore((s) => s.t);
  const openSettings = useSettingsStore((s) => s.openModal);
  const defaultWorkspace = useSettingsStore((s) => s.settings.general.default_workspace);

  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const createMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [wfList, runList] = await Promise.all([
          api.listWorkflows(),
          api.listRuns(),
        ]);
        if (cancelled) return;
        setWorkflows(wfList);
        setRuns(runList);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : t("wfList.loadFailed"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, [t]);

  const getRunCount = useCallback(
    (workflowId: string): number => runs.filter((r) => r.workflow_id === workflowId).length,
    [runs]
  );

  const handleCreate = useCallback(async () => {
    setCreating(true);
    try {
      const newWf = await api.createWorkflow({
        name: `${t("wfList.newWorkflow")} ${workflows.length + 1}`,
        description: "",
        workspace_directory: defaultWorkspace.trim() || undefined,
        mode: "auto",
      });
      router.push(`/workflows/${newWf.id}`);
    } catch (err) {
      console.error("Create workflow failed:", err);
    } finally {
      setCreating(false);
    }
  }, [defaultWorkspace, workflows.length, router, t]);

  const handleDelete = useCallback(
    async (id: string, e: React.MouseEvent) => {
      e.stopPropagation();
      if (!window.confirm(t("wfList.deleteConfirm"))) return;
      try {
        await api.deleteWorkflow(id);
        setWorkflows((prev) => prev.filter((w) => w.id !== id));
      } catch (err) {
        console.error("Delete workflow failed:", err);
      }
    },
    [t]
  );

  const handleOpen = useCallback((id: string) => router.push(`/workflows/${id}`), [router]);

  function formatTime(iso: string): string {
    const date = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    const diffHr = Math.floor(diffMin / 60);
    const diffDay = Math.floor(diffHr / 24);
    if (diffMin < 1) return t("wfList.justNow");
    if (diffMin < 60) return t("wfList.minAgo").replace("{n}", String(diffMin));
    if (diffHr < 24) return t("wfList.hrAgo").replace("{n}", String(diffHr));
    if (diffDay < 7) return t("wfList.dayAgo").replace("{n}", String(diffDay));
    return date.toLocaleDateString();
  }

  // ---- Loading ----
  if (loading) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-gray-50 to-gray-100 flex items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <div className="w-10 h-10 border-3 border-blue-500 border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-gray-500 font-medium">{t("wfList.loading")}</p>
        </div>
      </div>
    );
  }

  // ---- Error ----
  if (error) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-gray-50 to-gray-100 flex items-center justify-center">
        <div className="bg-white rounded-2xl shadow-lg p-8 max-w-md w-full text-center">
          <div className="w-16 h-16 bg-red-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
            <svg className="w-8 h-8 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
            </svg>
          </div>
          <h3 className="text-lg font-semibold text-gray-800 mb-2">{t("wfList.loadFailed")}</h3>
          <p className="text-sm text-gray-500 mb-6">{error}</p>
          <button
            onClick={() => window.location.reload()}
            className="px-6 py-2.5 text-sm font-medium bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors shadow-sm"
          >
            {t("wfList.retry")}
          </button>
        </div>
      </div>
    );
  }

  // ---- Empty ----
  if (workflows.length === 0) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-gray-50 to-blue-50/30 flex items-center justify-center p-6">
        <div className="bg-white rounded-2xl shadow-lg p-10 max-w-2xl w-full text-center">
          <div className="w-20 h-20 bg-gradient-to-br from-blue-100 to-indigo-100 rounded-2xl flex items-center justify-center mx-auto mb-6">
            <Workflow size={36} className="text-blue-500" />
          </div>
          <h2 className="text-2xl font-bold text-gray-800 mb-3">{t("wfList.emptyTitle")}</h2>
          <p className="text-gray-500 mb-8 leading-relaxed">{t("wfList.emptyDesc")}</p>

          <div className="max-w-sm mx-auto">
            <button
              onClick={() => handleCreate()}
              disabled={creating}
              className="group flex w-full flex-col items-center gap-3 p-6 rounded-xl border-2 border-blue-200 bg-gradient-to-br from-blue-50 to-indigo-50 hover:border-blue-400 hover:shadow-lg transition-all disabled:opacity-50"
            >
              <div className="w-12 h-12 rounded-xl bg-blue-100 flex items-center justify-center group-hover:bg-blue-200 transition-colors">
                <Target size={24} className="text-blue-600" />
              </div>
              <span className="text-sm font-semibold text-blue-700">
                {t("workflow.modeAuto")}
              </span>
              <span className="text-xs text-blue-500 leading-relaxed">
                {t("workflow.modeAutoDesc")}
              </span>
            </button>

          </div>
        </div>
      </div>
    );
  }

  // ---- Main ----
  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-50 to-gray-100">
      {/* Header */}
      <header className="bg-white/80 backdrop-blur-sm border-b border-gray-200 sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center shadow-sm">
              <span className="text-white text-sm font-bold">MA</span>
            </div>
            <div>
              <h1 className="text-lg font-bold text-gray-900">{t("toolbar.appTitle")}</h1>
              <p className="text-xs text-gray-400">AI Multi-Agent Workflow Platform</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={openSettings}
              className="p-2.5 rounded-xl text-gray-500 hover:text-gray-700 hover:bg-gray-100 transition-colors"
              title={t("settings.title")}
            >
              <Settings size={20} />
            </button>
            <div className="relative" ref={createMenuRef}>
              <button
                onClick={() => handleCreate()}
                disabled={creating}
                className="inline-flex items-center gap-2 px-5 py-2.5 text-sm font-semibold rounded-xl bg-gradient-to-r from-blue-600 to-indigo-600 text-white hover:from-blue-700 hover:to-indigo-700 transition-all shadow-sm hover:shadow-md disabled:opacity-50"
              >
                <Plus size={16} />
                {t("wfList.newWorkflow")}
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Grid */}
      <main className="max-w-7xl mx-auto px-6 py-8">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
          {workflows.map((wf) => (
            <div
              key={wf.id}
              onClick={() => handleOpen(wf.id)}
              className="group bg-white rounded-xl border border-gray-200 p-6 cursor-pointer hover:border-blue-300 hover:shadow-lg transition-all duration-200 relative overflow-hidden"
            >
              {/* Accent bar */}
              <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-blue-500 to-indigo-500 opacity-0 group-hover:opacity-100 transition-opacity" />

              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-2">
                    <div className="w-8 h-8 rounded-lg bg-blue-50 flex items-center justify-center shrink-0">
                      <Workflow size={16} className="text-blue-500" />
                    </div>
                    <h3 className="text-sm font-semibold text-gray-800 truncate group-hover:text-blue-600 transition-colors">
                      {wf.name}
                    </h3>
                    {/* Mode badge */}
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-blue-100 text-blue-700">
                      <Target size={10} />
                      {t("workflow.modeAuto")}
                    </span>
                  </div>
                  {wf.description && (
                    <p className="text-xs text-gray-400 line-clamp-2 ml-10">{wf.description}</p>
                  )}
                </div>
                <button
                  onClick={(e) => handleDelete(wf.id, e)}
                  className="p-2 rounded-lg text-gray-300 hover:text-red-500 hover:bg-red-50 opacity-0 group-hover:opacity-100 transition-all"
                  title={t("wfList.deleteTooltip")}
                >
                  <Trash2 size={14} />
                </button>
              </div>

              <div className="flex items-center justify-between mt-5 pt-4 border-t border-gray-100">
                <div className="flex items-center gap-4 text-xs text-gray-400">
                  <span className="flex items-center gap-1.5">
                    <Clock size={12} />
                    {formatTime(wf.updated_at)}
                  </span>
                  <span className="flex items-center gap-1.5">
                    <Play size={12} />
                    {t("wfList.runs").replace("{n}", String(getRunCount(wf.id)))}
                  </span>
                </div>
                <ArrowRight size={16} className="text-gray-300 group-hover:text-blue-500 transition-colors" />
              </div>
            </div>
          ))}
        </div>
      </main>

      {/* Global Settings Modal */}
      <SettingsModal />
    </div>
  );
}
