"use client";

import { useEffect, useMemo, useRef } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useLocaleStore } from "@/stores/localeStore";
import type { StreamEvent } from "@/types/events";

// ---------------------------------------------------------------------------
// CommunicationPanel — shows upstream/downstream data for the selected node
// ---------------------------------------------------------------------------

interface CommRecord {
  timestamp: number;
  direction: "in" | "out";
  summary: string;
  eventType: string;
}

export default function CommunicationPanel({ nodeId }: { nodeId: string }) {
  const t = useLocaleStore((s) => s.t);
  const scrollRef = useRef<HTMLDivElement>(null);
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const edges = useWorkflowStore((s) => s.edges) ?? [];
  const allEvents = useRunStore((s) => s.events);
  const tasks = useTaskStore((s) => s.tasks);
  const taskMessages = useTaskStore((s) => s.taskMessages);

  // Get the effective node ID: use provided nodeId or the store's selectedRunNodeId
  const selectedRunNodeId = useRunStore((s) => s.selectedRunNodeId);
  const effectiveNodeId = nodeId || selectedRunNodeId || "";

  // Get child node IDs for planner nodes
  const parentChildMap = useRunStore((s) => s.parentChildMap);
  const childIds = parentChildMap[effectiveNodeId] ?? [];

  // Find upstream and downstream node IDs
  const upstreamNodeIds = useMemo(
    () =>
      edges
        .filter((e) => e.target === effectiveNodeId)
        .map((e) => e.source),
    [edges, effectiveNodeId]
  );

  // Build communication records
  const records = useMemo<CommRecord[]>(() => {
    const result: CommRecord[] = [];

    // Helper to create summary from event
    function summarize(event: StreamEvent, direction: "in" | "out"): string {
      const content = "content" in event ? String((event as { content?: unknown }).content ?? "") : "";
      const source = event.node_id
        ? (nodes.find((n) => n.id === event.node_id)?.data as { label?: string } | undefined)?.label ?? event.node_id
        : "";

      switch (event.type) {
        case "llm_token":
        case "llm_chunk":
          return direction === "in"
            ? `${t("comm.receivedLlm")} ${source}`
            : t("comm.sentLlm");
        case "tool_call":
          return direction === "in"
            ? `${t("comm.receivedToolCall")} ${source}`
            : `${t("comm.sentToolCall")}: ${content.slice(0, 60)}`;
        case "tool_result":
          return direction === "in"
            ? t("comm.receivedToolResult")
            : `${t("comm.sentToolResult")}: ${content.slice(0, 60)}`;
        case "child_created":
          return direction === "out"
            ? t("comm.createdChild")
            : t("comm.receivedChild");
        case "child_completed":
          return `${t("comm.childCompleted")}: ${content.slice(0, 60)}`;
        default:
          return content ? content.slice(0, 80) : event.type;
      }
    }

    // Incoming: events from upstream nodes
    for (const upId of upstreamNodeIds) {
      const upEvents = allEvents.filter((e) => e.node_id === upId);
      for (const evt of upEvents) {
        result.push({
          timestamp: evt.timestamp,
          direction: "in",
          summary: summarize(evt, "in"),
          eventType: evt.type,
        });
      }
    }

    // Outgoing: events from this node
    const myEvents = allEvents.filter((e) => e.node_id === effectiveNodeId);
    for (const evt of myEvents) {
      result.push({
        timestamp: evt.timestamp,
        direction: "out",
        summary: summarize(evt, "out"),
        eventType: evt.type,
      });
    }

    // If this is a planner node, also show child node events
    if (childIds.length > 0) {
      const childEvents = allEvents.filter(
        (e) => e.node_id && childIds.includes(e.node_id)
      );
      for (const evt of childEvents) {
        const childLabel = nodes.find((n) => n.id === evt.node_id)?.data?.label || evt.node_id;
        result.push({
          timestamp: evt.timestamp,
          direction: "out",
          summary: `[${childLabel}] ${evt.type}: ${(evt as { content?: string }).content?.slice(0, 60) || ""}`,
          eventType: evt.type,
        });
      }
    }

    for (const task of tasks) {
      const messages = taskMessages[task.id] ?? [];
      const taskNodeId = task.assigned_node_id || "";
      for (const msg of messages) {
        const related =
          !effectiveNodeId ||
          taskNodeId === effectiveNodeId ||
          msg.sender_id === effectiveNodeId ||
          msg.target_node_id === effectiveNodeId;
        if (!related) continue;
        result.push({
          timestamp: new Date(msg.created_at).getTime(),
          direction: msg.sender_id === effectiveNodeId ? "out" : "in",
          summary: `${msg.sender_type}:${msg.sender_id}${msg.target_node_id ? ` -> ${msg.target_node_id}` : ""}: ${msg.content.slice(0, 120)}`,
          eventType: msg.message_type,
        });
      }
    }

    // Sort by timestamp
    result.sort((a, b) => a.timestamp - b.timestamp);
    return result;
  }, [allEvents, effectiveNodeId, upstreamNodeIds, nodes, t, childIds, tasks, taskMessages]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [records.length]);

  // Format timestamp
  function formatTime(ts: number): string {
    // Handle both seconds and milliseconds
    const ms = ts > 1e12 ? ts : ts > 1e9 ? ts * 1000 : ts;
    const d = new Date(ms);
    return d.toLocaleTimeString("zh-CN", { hour12: false });
  }

  if (!effectiveNodeId) {
    return (
      <div className="h-full flex items-center justify-center text-gray-400 text-xs">
        {t("comm.selectNode")}
      </div>
    );
  }

  if (records.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-gray-400 text-xs">
        {t("comm.noRecords")}
      </div>
    );
  }

  return (
    <div ref={scrollRef} className="h-full overflow-auto p-2 space-y-1">
      {records.map((rec, i) => (
        <div
          key={i}
          className="flex items-start gap-2 px-2 py-1.5 rounded hover:bg-gray-50 text-xs"
        >
          {/* Direction icon */}
          {rec.direction === "in" ? (
            <ArrowDown size={13} className="text-blue-500 mt-0.5 shrink-0" />
          ) : (
            <ArrowUp size={13} className="text-green-500 mt-0.5 shrink-0" />
          )}
          {/* Timestamp */}
          <span className="text-gray-400 font-mono shrink-0 w-16">
            {formatTime(rec.timestamp)}
          </span>
          {/* Event type badge */}
          <span className="inline-block rounded bg-gray-100 px-1 py-0.5 text-gray-500 font-mono shrink-0">
            {rec.eventType}
          </span>
          {/* Summary */}
          <span className="text-gray-700 truncate">{rec.summary}</span>
        </div>
      ))}
    </div>
  );
}
