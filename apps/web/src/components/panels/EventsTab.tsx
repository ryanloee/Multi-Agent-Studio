"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Minus,
  Brain,
  Terminal,
  Wrench,
  MessageSquare,
  FileText,
  Activity,
  Layers,
} from "lucide-react";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useLocaleStore } from "@/stores/localeStore";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
type EventKind = "all" | "llm" | "tool" | "shell" | "comm" | "status";

interface TimelineItem {
  id: string;
  ts: number;
  nodeId: string;
  kind: EventKind;
  title: string;
  content: string;
  direction: "in" | "out" | "self" | null;
  sourceId?: string;
  targetId?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function eventKind(type: string): EventKind {
  if (type === "llm_token" || type === "llm_chunk") return "llm";
  if (type === "tool_call" || type === "tool_result") return "tool";
  if (type === "shell_stdout" || type === "shell_stderr") return "shell";
  if (type === "task_message" || type === "worker_message" || type === "child_created" || type === "child_completed")
    return "comm";
  return "status";
}

function iconFor(kind: EventKind) {
  switch (kind) {
    case "llm":    return Brain;
    case "tool":   return Wrench;
    case "shell":  return Terminal;
    case "comm":   return MessageSquare;
    default:       return Activity;
  }
}

function formatTime(ts: number): string {
  const ms = ts > 1e12 ? ts : ts > 1e9 ? ts * 1000 : ts;
  return new Date(ms).toLocaleTimeString("zh-CN", { hour12: false });
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + "…" : s;
}

// ---------------------------------------------------------------------------
// EventsTab — unified event timeline with type filtering & direction arrows
// ---------------------------------------------------------------------------
export default function EventsTab({ nodeId = "" }: { nodeId?: string }) {
  const t = useLocaleStore((s) => s.t);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [filter, setFilter] = useState<EventKind>("all");

  const allEvents = useRunStore((s) => s.events);
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const edges = useWorkflowStore((s) => s.edges) ?? [];
  const tasks = useTaskStore((s) => s.tasks);
  const taskMessages = useTaskStore((s) => s.taskMessages);
  const parentChildMap = useRunStore((s) => s.parentChildMap);
  const nodeStatuses = useRunStore((s) => s.nodeStatuses);

  // Upstream node IDs for direction inference
  const upstreamIds = useMemo(
    () => new Set(edges.filter((e) => e.target === nodeId).map((e) => e.source)),
    [edges, nodeId]
  );

  // Child node IDs (for planner nodes)
  const childIds = useMemo(() => parentChildMap[nodeId] ?? [], [parentChildMap, nodeId]);

  const items = useMemo(() => {
    const result: TimelineItem[] = [];

    // Stream events
    for (const event of allEvents) {
      const kind = eventKind(event.type);
      const evNodeId = event.node_id || "";
      const rec = event as unknown as Record<string, unknown>;
      const content = String(rec.content ?? rec.title ?? event.type);

      // Node filtering
      if (nodeId && evNodeId !== nodeId) {
        // Also include events from upstream (in) and downstream (out) for context
        const isUpstream = upstreamIds.has(evNodeId);
        const isDownstream = childIds.includes(evNodeId);
        if (!isUpstream && !isDownstream) continue;
      }

      // Direction inference
      let direction: TimelineItem["direction"] = null;
      if (nodeId) {
        if (evNodeId === nodeId) {
          direction = "self";
        } else if (upstreamIds.has(evNodeId)) {
          direction = "in";
        } else if (childIds.includes(evNodeId)) {
          direction = "out";
        }
      }

      result.push({
        id: `ev-${event.event_id ?? result.length}`,
        ts: Number(rec.timestamp || Date.now()),
        nodeId: evNodeId,
        kind,
        title: event.type,
        content,
        direction,
        sourceId: evNodeId || undefined,
      });
    }

    // Task messages (inter-node communication with explicit direction)
    for (const task of tasks) {
      const messages = taskMessages[task.id] ?? [];
      for (const msg of messages) {
        const related =
          !nodeId ||
          msg.sender_id === nodeId ||
          msg.target_node_id === nodeId;
        if (!related) continue;

        const direction = !nodeId ? null
          : msg.sender_id === nodeId ? "out"
          : "in";

        result.push({
          id: `msg-${msg.id}`,
          ts: new Date(msg.created_at).getTime(),
          nodeId: msg.sender_id,
          kind: "comm",
          title: `${msg.sender_type}/${msg.message_type}`,
          content: truncate(msg.content, 200),
          direction,
          sourceId: msg.sender_id,
          targetId: msg.target_node_id || undefined,
        });
      }
    }

    // Artifacts
    const artifacts = useTaskStore.getState().artifacts;
    for (const art of artifacts) {
      if (nodeId && art.node_id !== nodeId) continue;
      result.push({
        id: `art-${art.id}`,
        ts: new Date(art.created_at).getTime(),
        nodeId: art.node_id || "",
        kind: "status",
        title: `${art.type}: ${art.title}`,
        content: "",
        direction: null,
      });
    }

    return result
      .filter((item) => filter === "all" || item.kind === filter)
      .sort((a, b) => a.ts - b.ts);
  }, [allEvents, nodeId, upstreamIds, childIds, tasks, taskMessages, filter]);

  // Auto-scroll
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [items.length]);

  const filters: { key: EventKind; label: string }[] = [
    { key: "all", label: t("events.filterAll") },
    { key: "llm", label: t("events.filterLlm") },
    { key: "tool", label: t("events.filterTool") },
    { key: "shell", label: t("events.filterShell") },
    { key: "comm", label: t("events.filterComm") },
    { key: "status", label: t("events.filterStatus") },
  ];

  // Node label lookup
  const nodeLabel = (id: string) => {
    const n = nodes.find((n) => n.id === id);
    return n ? ((n.data as { label?: string }).label || id) : id;
  };

  if (items.length === 0) {
    return (
      <div className="h-full flex flex-col bg-white">
        <div className="flex items-center gap-1 border-b border-gray-100 px-2 py-1.5 shrink-0">
          {filters.map((f) => (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={`rounded px-2 py-0.5 text-[10px] font-medium ${
                filter === f.key ? "bg-gray-900 text-white" : "bg-gray-100 text-gray-500 hover:bg-gray-200"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex-1 flex items-center justify-center text-xs text-gray-400">
          {t("events.noEvents")}
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Filter bar */}
      <div className="flex items-center gap-1 border-b border-gray-100 px-2 py-1.5 shrink-0">
        {filters.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`rounded px-2 py-0.5 text-[10px] font-medium ${
              filter === f.key ? "bg-gray-900 text-white" : "bg-gray-100 text-gray-500 hover:bg-gray-200"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Timeline */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-2">
        <div className="space-y-1">
          {items.map((item) => {
            const Icon = iconFor(item.kind);
            return (
              <div
                key={item.id}
                className="flex items-start gap-2 px-2 py-1.5 rounded hover:bg-gray-50 text-xs group"
              >
                {/* Direction arrow */}
                {item.direction === "in" ? (
                  <ArrowDown size={13} className="text-blue-500 mt-0.5 shrink-0" />
                ) : item.direction === "out" ? (
                  <ArrowUp size={13} className="text-green-500 mt-0.5 shrink-0" />
                ) : item.direction === "self" ? (
                  <Minus size={13} className="text-gray-400 mt-0.5 shrink-0" />
                ) : (
                  <Icon size={13} className="text-gray-400 mt-0.5 shrink-0" />
                )}

                {/* Timestamp */}
                <span className="text-gray-400 font-mono shrink-0 w-14">
                  {formatTime(item.ts)}
                </span>

                {/* Event type badge */}
                <span className="inline-block rounded bg-gray-100 px-1 py-0.5 text-[10px] text-gray-500 font-mono shrink-0">
                  {item.title}
                </span>

                {/* Node label (when showing multi-node) */}
                {item.nodeId && (!nodeId || item.direction !== "self") && (
                  <span className="text-[10px] text-blue-400 font-mono shrink-0">
                    {nodeLabel(item.nodeId)}
                    {item.targetId && (
                      <> → {nodeLabel(item.targetId)}</>
                    )}
                  </span>
                )}

                {/* Content preview */}
                {item.content && (
                  <span className="text-gray-600 truncate min-w-0">
                    {truncate(item.content, 150)}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
