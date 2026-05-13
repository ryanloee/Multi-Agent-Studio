"use client";

import { useState, useMemo, useCallback, useEffect, useRef } from "react";
import {
  ChevronRight,
  ChevronDown,
  Loader2,
  CheckCircle2,
  XCircle,
} from "lucide-react";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import type {
  ToolCallEvent,
  ToolResultEvent,
} from "@/types/events";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
interface ToolCallListProps {
  /** Filter events to a specific node. Empty string = all nodes. */
  nodeId?: string;
}

// ---------------------------------------------------------------------------
// Paired entry — matches a tool_call with its corresponding tool_result
// ---------------------------------------------------------------------------
interface PairedToolCall {
  call: ToolCallEvent;
  result: ToolResultEvent | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format a unix timestamp into a concise HH:MM:SS string */
function formatTimestamp(ts: number): string {
  // Support both seconds and milliseconds
  const ms = ts > 1e12 ? ts : ts * 1000;
  const d = new Date(ms);
  return d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Derive display status from the pair */
function getPairStatus(
  pair: PairedToolCall
): "running" | "completed" | "failed" {
  if (!pair.result) return "running";
  // If result content indicates an error, mark as failed
  const content = pair.result.content ?? "";
  if (
    content.toLowerCase().includes("error") ||
    content.toLowerCase().includes("failed")
  ) {
    return "failed";
  }
  return "completed";
}

// ---------------------------------------------------------------------------
// StatusIcon
// ---------------------------------------------------------------------------
function StatusIcon({ status }: { status: "running" | "completed" | "failed" }) {
  switch (status) {
    case "running":
      return <Loader2 size={14} className="text-blue-500 animate-spin" />;
    case "completed":
      return <CheckCircle2 size={14} className="text-green-500" />;
    case "failed":
      return <XCircle size={14} className="text-red-500" />;
  }
}

// ---------------------------------------------------------------------------
// ToolCallItem — single expandable row
// ---------------------------------------------------------------------------
function ToolCallItem({ pair }: { pair: PairedToolCall }) {
  const t = useLocaleStore((s) => s.t);
  const [expanded, setExpanded] = useState(false);

  const status = getPairStatus(pair);
  const time = formatTimestamp(pair.call.timestamp);
  const toolName = pair.call.tool_name || "unknown";
  const nodeId = pair.call.node_id || "-";

  const toggle = useCallback(() => setExpanded((v) => !v), []);

  return (
    <div className="border-b border-gray-100 last:border-b-0">
      {/* ---- Summary row ---- */}
      <button
        onClick={toggle}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-50 transition-colors text-xs"
      >
        {/* Expand chevron */}
        {expanded ? (
          <ChevronDown size={12} className="text-gray-400 shrink-0" />
        ) : (
          <ChevronRight size={12} className="text-gray-400 shrink-0" />
        )}

        {/* Status icon */}
        <StatusIcon status={status} />

        {/* Timestamp */}
        <span className="text-gray-400 font-mono shrink-0">{time}</span>

        {/* Tool name */}
        <span className="font-medium text-gray-700 truncate">{toolName}</span>

        {/* Node ID */}
        {nodeId && nodeId !== "-" && (
          <span className="ml-auto text-gray-400 shrink-0 truncate max-w-[120px]">
            {nodeId}
          </span>
        )}
      </button>

      {/* ---- Expandable detail ---- */}
      {expanded && (
        <div className="px-8 pb-3 space-y-2">
          {/* Tool call content */}
          {pair.call.content && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-1">
                {t("tools.call")}
              </div>
              <pre className="bg-gray-50 rounded px-3 py-2 text-xs text-gray-700 whitespace-pre-wrap break-all max-h-40 overflow-auto font-mono">
                {pair.call.content}
              </pre>
            </div>
          )}

          {/* Tool result content */}
          {pair.result?.content && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-1">
                {t("tools.result")}
              </div>
              <pre className="bg-gray-50 rounded px-3 py-2 text-xs text-gray-700 whitespace-pre-wrap break-all max-h-40 overflow-auto font-mono">
                {pair.result.content}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ToolCallList
// ---------------------------------------------------------------------------
export default function ToolCallList({ nodeId = "" }: ToolCallListProps) {
  const t = useLocaleStore((s) => s.t);
  const scrollRef = useRef<HTMLDivElement>(null);
  const allEvents = useRunStore((s) => s.events);
  const events = useMemo(
    () =>
      allEvents.filter(
        (e) =>
          (e.type === "tool_call" || e.type === "tool_result") &&
          (nodeId === "" || e.node_id === nodeId)
      ),
    [allEvents, nodeId]
  );

  // Pair tool_calls with their results by matching tool_name + node_id + proximity
  const pairs = useMemo(() => {
    const result: PairedToolCall[] = [];
    const pendingCalls: ToolCallEvent[] = [];

    for (const ev of events) {
      if (ev.type === "tool_call") {
        pendingCalls.push(ev as ToolCallEvent);
      } else if (ev.type === "tool_result") {
        const resultEv = ev as ToolResultEvent;
        // Find the earliest unmatched call with the same tool_name + node_id
        const idx = pendingCalls.findIndex(
          (c) => c.tool_name === resultEv.tool_name && c.node_id === resultEv.node_id
        );
        if (idx !== -1) {
          const call = pendingCalls.splice(idx, 1)[0];
          result.push({ call, result: resultEv });
        } else {
          // Orphan result — still show it
          result.push({
            call: {
              ...resultEv,
              type: "tool_call",
              content: "",
            } as ToolCallEvent,
            result: resultEv,
          });
        }
      }
    }

    // Remaining unmatched calls (still running)
    for (const call of pendingCalls) {
      result.push({ call, result: null });
    }

    return result;
  }, [events]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [pairs.length, events.length]);

  if (pairs.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-gray-400">
        {t("tools.noCalls")}
      </div>
    );
  }

  return (
    <div ref={scrollRef} className="w-full h-full overflow-y-auto divide-y divide-gray-50">
      {pairs.map((pair, idx) => (
        <ToolCallItem key={`${pair.call.timestamp}-${idx}`} pair={pair} />
      ))}
    </div>
  );
}
