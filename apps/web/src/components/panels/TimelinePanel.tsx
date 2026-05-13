"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { FileText, MessageSquare, Terminal, Wrench, Brain, Activity } from "lucide-react";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";

type TimelineKind = "all" | "llm" | "tool" | "shell" | "comm" | "artifact" | "status";

function eventKind(type: string): TimelineKind {
  if (type === "llm_token" || type === "llm_chunk") return "llm";
  if (type === "tool_call" || type === "tool_result") return "tool";
  if (type === "shell_stdout" || type === "shell_stderr") return "shell";
  if (type === "task_message" || type === "worker_message" || type === "planner_guidance") return "comm";
  if (type === "artifact_created") return "artifact";
  return "status";
}

function iconFor(kind: TimelineKind) {
  if (kind === "llm") return Brain;
  if (kind === "tool") return Wrench;
  if (kind === "shell") return Terminal;
  if (kind === "comm") return MessageSquare;
  if (kind === "artifact") return FileText;
  return Activity;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

export default function TimelinePanel({ nodeId }: { nodeId?: string }) {
  const [filter, setFilter] = useState<TimelineKind>("all");
  const scrollRef = useRef<HTMLDivElement>(null);
  const events = useRunStore((s) => s.events);
  const artifacts = useTaskStore((s) => s.artifacts);
  const taskMessages = useTaskStore((s) => s.taskMessages);

  const items = useMemo(() => {
    const eventItems = events.map((event, idx) => {
      const rec = asRecord(event);
      const kind = eventKind(event.type);
      const content = String(rec.content ?? rec.title ?? event.type);
      return {
        id: `event-${idx}`,
        ts: Number(rec.timestamp || Date.now()),
        nodeId: event.node_id,
        kind,
        title: event.type,
        content,
      };
    });

    const artifactItems = artifacts.map((artifact) => ({
      id: `artifact-${artifact.id}`,
      ts: new Date(artifact.created_at).getTime(),
      nodeId: artifact.node_id || "",
      kind: "artifact" as TimelineKind,
      title: `${artifact.type}: ${artifact.title}`,
      content: artifact.content,
    }));

    const messageItems = Object.values(taskMessages).flat().map((message) => ({
      id: `message-${message.id}`,
      ts: new Date(message.created_at).getTime(),
      nodeId: message.sender_id,
      kind: "comm" as TimelineKind,
      title: `${message.sender_type} / ${message.message_type}`,
      content: message.content,
    }));

    return [...eventItems, ...artifactItems, ...messageItems]
      .filter((item) => !nodeId || item.nodeId === nodeId)
      .filter((item) => filter === "all" || item.kind === filter)
      .sort((a, b) => a.ts - b.ts);
  }, [artifacts, events, filter, nodeId, taskMessages]);

  const filters: TimelineKind[] = ["all", "llm", "tool", "shell", "comm", "artifact", "status"];

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [items.length, filter]);

  return (
    <div className="h-full flex flex-col bg-white">
      <div className="flex items-center gap-1 border-b border-gray-100 px-2 py-1.5 shrink-0">
        {filters.map((key) => (
          <button
            key={key}
            onClick={() => setFilter(key)}
            className={`rounded px-2 py-0.5 text-[10px] font-medium ${
              filter === key ? "bg-gray-900 text-white" : "bg-gray-100 text-gray-500 hover:bg-gray-200"
            }`}
          >
            {key}
          </button>
        ))}
      </div>
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-2">
        {items.length === 0 ? (
          <div className="py-8 text-center text-xs text-gray-400">No timeline events</div>
        ) : (
          <div className="space-y-2">
            {items.map((item) => {
              const Icon = iconFor(item.kind);
              return (
                <div key={item.id} className="flex gap-2 text-xs">
                  <Icon size={13} className="mt-0.5 shrink-0 text-gray-400" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-gray-700 truncate">{item.title}</span>
                      {item.nodeId && <span className="text-[10px] text-gray-400">{item.nodeId}</span>}
                    </div>
                    {item.content && (
                      <div className="mt-0.5 line-clamp-4 whitespace-pre-wrap text-[11px] leading-relaxed text-gray-500">
                        {item.content}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
