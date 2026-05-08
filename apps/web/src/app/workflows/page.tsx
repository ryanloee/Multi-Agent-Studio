"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Plus, Trash2, Workflow, Clock, Play } from "lucide-react";
import { api } from "@/lib/api";
import { useLocaleStore } from "@/stores/localeStore";
import type { WorkflowSummary, RunInfo } from "@/types/api";

// ---------------------------------------------------------------------------
// WorkflowListPage — grid card layout of all workflows
// ---------------------------------------------------------------------------

export default function WorkflowListPage() {
  const router = useRouter();
  const t = useLocaleStore((s) => s.t);

  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // ---- Load workflows + runs on mount ----
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
        setError(err instanceof Error ? err.message : "Failed to load workflows");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, []);

  // ---- Count runs per workflow ----
  const getRunCount = useCallback(
    (workflowId: string): number => {
      return runs.filter((r) => r.workflow_id === workflowId).length;
    },
    [runs]
  );

  // ---- Create new workflow ----
  const handleCreate = useCallback(async () => {
    setCreating(true);
    try {
      const newWf = await api.createWorkflow({
        name: `Workflow ${workflows.length + 1}`,
        description: "",
      });
      router.push(`/workflows/${newWf.id}`);
    } catch (err) {
      console.error("Create workflow failed:", err);
    } finally {
      setCreating(false);
    }
  }, [workflows.length, router]);

  // ---- Delete workflow ----
  const handleDelete = useCallback(
    async (id: string, e: React.MouseEvent) => {
      e.stopPropagation();
      const confirmed = window.confirm(t("wfList.deleteConfirm"));
      if (!confirmed) return;

      try {
        await api.deleteWorkflow(id);
        setWorkflows((prev) => prev.filter((w) => w.id !== id));
      } catch (err) {
        console.error("Delete workflow failed:", err);
      }
    },
    [t]
  );

  // ---- Navigate to editor ----
  const handleOpen = useCallback(
    (id: string) => {
      router.push(`/workflows/${id}`);
    },
    [router]
  );

  // ---- Format relative time ----
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

  // ---- Loading state ----
  if (loading) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center space-y-3">
          <div className="w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full animate-spin mx-auto" />
          <p className="text-sm text-gray-500">{t("wfList.loading")}</p>
        </div>
      </div>
    );
  }

  // ---- Error state ----
  if (error) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center space-y-3">
          <p className="text-red-500 font-medium">{t("wfList.loadFailed")}</p>
          <p className="text-sm text-gray-400">{error}</p>
          <button
            onClick={() => window.location.reload()}
            className="px-4 py-2 text-sm bg-blue-500 text-white rounded-md hover:bg-blue-600 transition-colors"
          >
            {t("wfList.retry")}
          </button>
        </div>
      </div>
    );
  }

  // ---- Empty state ----
  if (workflows.length === 0) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center space-y-5 max-w-md">
          <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-blue-100 to-indigo-100 flex items-center justify-center mx-auto">
            <Workflow size={32} className="text-blue-500" />
          </div>
          <div className="space-y-2">
            <h2 className="text-xl font-semibold text-gray-800">
              {t("wfList.emptyTitle")}
            </h2>
            <p className="text-sm text-gray-400">
              {t("wfList.emptyDesc")}
            </p>
          </div>
          <button
            onClick={handleCreate}
            disabled={creating}
            className="inline-flex items-center gap-2 px-5 py-2.5 text-sm font-medium rounded-lg bg-blue-600 text-white hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            <Plus size={16} />
            {t("wfList.createFirst")}
          </button>
        </div>
      </div>
    );
  }

  // ---- Main grid layout ----
  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-6 py-4">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-md bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center">
              <span className="text-white text-xs font-bold">MA</span>
            </div>
            <h1 className="text-lg font-semibold text-gray-800">
              {t("toolbar.appTitle")}
            </h1>
          </div>
          <button
            onClick={handleCreate}
            disabled={creating}
            className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-md bg-blue-600 text-white hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            <Plus size={16} />
            {t("wfList.newWorkflow")}
          </button>
        </div>
      </header>

      {/* Workflow grid */}
      <main className="max-w-7xl mx-auto px-6 py-6">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {workflows.map((wf) => (
            <div
              key={wf.id}
              onClick={() => handleOpen(wf.id)}
              className="group bg-white rounded-xl border border-gray-200 p-5 cursor-pointer hover:border-blue-300 hover:shadow-md transition-all"
            >
              {/* Card header */}
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <h3 className="text-sm font-semibold text-gray-800 truncate group-hover:text-blue-600 transition-colors">
                    {wf.name}
                  </h3>
                  {wf.description && (
                    <p className="text-xs text-gray-400 mt-1 line-clamp-2">
                      {wf.description}
                    </p>
                  )}
                </div>
                <button
                  onClick={(e) => handleDelete(wf.id, e)}
                  className="p-1.5 rounded-md text-gray-300 hover:text-red-500 hover:bg-red-50 opacity-0 group-hover:opacity-100 transition-all"
                  title={t("wfList.deleteTooltip")}
                >
                  <Trash2 size={14} />
                </button>
              </div>

              {/* Card footer: metadata */}
              <div className="flex items-center gap-4 mt-4 text-xs text-gray-400">
                <div className="flex items-center gap-1">
                  <Clock size={12} />
                  {formatTime(wf.updated_at)}
                </div>
                <div className="flex items-center gap-1">
                  <Play size={12} />
                  {t("wfList.runs").replace("{n}", String(getRunCount(wf.id)))}
                </div>
              </div>
            </div>
          ))}
        </div>
      </main>
    </div>
  );
}
