"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import type { DirectorDecisionEvent } from "@/types/events";
import type { RunStatus } from "@/types/workflow";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
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
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

// ---------------------------------------------------------------------------
// EventsTab — vertical timeline moved from FlowCanvas
// ---------------------------------------------------------------------------
export default function EventsTab({ nodeId = "" }: { nodeId?: string }) {
  const t = useLocaleStore((s) => s.t);
  const decisions = useRunStore((s) => s.directorDecisions);
  const allEvents = useRunStore((s) => s.events);
  const setSelectedRunNode = useRunStore((s) => s.setSelectedRunNode);
  const scrollRef = useRef<HTMLDivElement>(null);

  const handleNodeClick = useCallback(
    (id: string) => {
      setSelectedRunNode(id);
    },
    [setSelectedRunNode],
  );

  const nodeEvents = useMemo(
    () =>
      allEvents.filter(
        (e) =>
          e.type === "node_started" ||
          e.type === "node_completed" ||
          e.type === "node_failed" ||
          e.type === "director_decision",
      ),
    [allEvents],
  );

  // Filter by nodeId if provided
  const filteredEvents = useMemo(
    () =>
      nodeId
        ? nodeEvents.filter((e) => e.node_id === nodeId || e.type === "director_decision")
        : nodeEvents,
    [nodeEvents, nodeId],
  );

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [filteredEvents.length]);

  if (filteredEvents.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-xs text-gray-400 bg-white">
        {t("events.noEvents")}
      </div>
    );
  }

  return (
    <div ref={scrollRef} className="h-full overflow-y-auto px-4 py-3 space-y-1 bg-white">
      <div className="relative">
        <div className="absolute left-[19px] top-0 bottom-0 w-0.5 bg-gray-200" />
        {filteredEvents.map((event, idx) => {
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
                    <span className="text-xs font-mono text-gray-400">
                      {formatTimestamp(event.timestamp)}
                    </span>
                    <span className="text-xs font-medium px-1.5 py-0.5 rounded bg-purple-100 text-purple-700">
                      {de.action}
                    </span>
                    {de.iteration !== undefined && (
                      <span className="text-xs text-gray-400">#{de.iteration}</span>
                    )}
                  </div>
                  <p className="text-sm text-gray-700 mt-0.5 line-clamp-2">{de.reasoning}</p>
                  {de.target_files && de.target_files.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-1">
                      {de.target_files.map((f) => (
                        <span key={f} className="text-xs px-1.5 py-0.5 bg-gray-100 text-gray-600 rounded">
                          {f}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          }

          const status: RunStatus =
            event.type === "node_started"
              ? "running"
              : event.type === "node_completed"
                ? "completed"
                : "failed";
          const colorClass = STATUS_COLORS[status] || STATUS_COLORS.pending;
          const statusLabel = status === "running" ? "Running" : status === "completed" ? "Done" : "Failed";

          return (
            <div
              key={`node-${idx}`}
              className="relative flex items-start gap-3 py-2 cursor-pointer group hover:bg-gray-50 rounded-lg -mx-2 px-2"
              onClick={() => handleNodeClick(event.node_id)}
            >
              <div
                className={`relative z-10 w-10 h-10 rounded-full border-2 flex items-center justify-center text-xs font-bold shrink-0 ${colorClass}`}
              >
                {status === "running" ? "..." : statusLabel[0]}
              </div>
              <div className="flex-1 min-w-0 pt-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-800 truncate">{event.node_id}</span>
                  <span className={`text-xs font-medium px-1.5 py-0.5 rounded ${colorClass}`}>
                    {statusLabel}
                  </span>
                </div>
                <span className="text-xs font-mono text-gray-400">
                  {formatTimestamp(event.timestamp)}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
