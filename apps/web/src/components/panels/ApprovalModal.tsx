"use client";

import { useState, useEffect, useCallback } from "react";
import { ShieldCheck, ShieldX, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
interface ApprovalModalProps {
  /** The run ID that is paused and awaiting approval */
  runId: string;
}

// ---------------------------------------------------------------------------
// ApprovalModal — full-screen overlay modal for Human-in-the-Loop approval
//
// - Shows a diff view with line-by-line coloring (+ green, - red)
// - Approve / Reject buttons
// - Closes automatically when status changes away from "paused"
// ---------------------------------------------------------------------------
export default function ApprovalModal({ runId }: ApprovalModalProps) {
  const setStatus = useRunStore((s) => s.setStatus);
  const setRunId = useRunStore((s) => s.setRunId);
  const t = useLocaleStore((s) => s.t);

  const [diffText, setDiffText] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ---- Load diff ----
  useEffect(() => {
    let cancelled = false;

    async function loadDiff() {
      setLoading(true);
      setError(null);
      try {
        const text = await api.getRunDiff(runId);
        if (cancelled) return;
        setDiffText(text);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load diff");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    loadDiff();
    return () => {
      cancelled = true;
    };
  }, [runId]);

  // ---- Approve handler ----
  const handleApprove = useCallback(async () => {
    setActionLoading(true);
    try {
      await api.approveRun(runId);
      setStatus("running");
    } catch (err) {
      console.error("Approve failed:", err);
    } finally {
      setActionLoading(false);
    }
  }, [runId, setStatus]);

  // ---- Reject handler ----
  const handleReject = useCallback(async () => {
    setActionLoading(true);
    try {
      await api.rejectRun(runId);
      setStatus("failed");
      setRunId(null);
    } catch (err) {
      console.error("Reject failed:", err);
    } finally {
      setActionLoading(false);
    }
  }, [runId, setStatus, setRunId]);

  // ---- Parse diff into colored lines ----
  const diffLines = diffText.split("\n");

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-2xl max-h-[80vh] flex flex-col mx-4">
        {/* ---- Header ---- */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 shrink-0">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-yellow-100 flex items-center justify-center">
              <ShieldCheck size={18} className="text-yellow-600" />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-gray-800">
                {t("approval.title")}
              </h2>
              <p className="text-xs text-gray-400 mt-0.5">
                {t("approval.subtitle")}
              </p>
            </div>
          </div>
          {/* Note: no close button — user must approve or reject */}
        </div>

        {/* ---- Diff content ---- */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 size={20} className="animate-spin text-gray-400" />
              <span className="ml-2 text-sm text-gray-400">
                {t("approval.loading")}
              </span>
            </div>
          ) : error ? (
            <div className="text-center py-12">
              <p className="text-sm text-red-500">{error}</p>
            </div>
          ) : diffText ? (
            <div className="bg-gray-900 rounded-lg overflow-hidden">
              <div className="px-4 py-2 bg-gray-800 border-b border-gray-700">
                <span className="text-xs text-gray-400 font-mono">
                  {t("approval.changes")}
                </span>
              </div>
              <div className="p-4 overflow-x-auto">
                <pre className="text-xs font-mono leading-5">
                  {diffLines.map((line, idx) => {
                    // Determine line styling
                    let lineClass = "text-gray-300"; // default context line

                    if (line.startsWith("+++") || line.startsWith("---")) {
                      lineClass = "text-gray-500 font-bold";
                    } else if (line.startsWith("+")) {
                      lineClass = "text-green-400 bg-green-900/20";
                    } else if (line.startsWith("-")) {
                      lineClass = "text-red-400 bg-red-900/20";
                    } else if (line.startsWith("@@")) {
                      lineClass = "text-blue-400";
                    }

                    return (
                      <div
                        key={idx}
                        className={`${lineClass} px-2 -mx-2 rounded-sm`}
                      >
                        {line}
                      </div>
                    );
                  })}
                </pre>
              </div>
            </div>
          ) : (
            <div className="text-center py-12">
              <p className="text-sm text-gray-400">{t("approval.noChanges")}</p>
            </div>
          )}
        </div>

        {/* ---- Footer: Approve / Reject buttons ---- */}
        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-gray-200 shrink-0">
          <button
            onClick={handleReject}
            disabled={actionLoading}
            className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-md border border-red-300 text-red-600 hover:bg-red-50 transition-colors disabled:opacity-50"
          >
            <ShieldX size={16} />
            {t("approval.reject")}
          </button>
          <button
            onClick={handleApprove}
            disabled={actionLoading}
            className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-md bg-green-600 text-white hover:bg-green-700 transition-colors disabled:opacity-50"
          >
            {actionLoading ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <ShieldCheck size={16} />
            )}
            {t("approval.approve")}
          </button>
        </div>
      </div>
    </div>
  );
}
